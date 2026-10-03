"""Optional native FDTD stepper: blocks of Yee steps in C, loaded with ctypes (backend "native").

The C side (``native/fdtd.c``) runs `engine.Simulation`'s steps (sweeps with the CPML, the
Crank-Nicolson coefficients, the copper-edge μ planes, the inductive sheet, sources and DTFT
probes) for a block of steps at a time, on a pool of threads, with every value computed by the
numpy reference's operations in the same order and no floating-point contraction: a float64 run
is bit-identical to the numpy backend, for any thread count and instruction set. float32 is the
same code with float fields (equal to numpy float32).

Selection (docs/rf/solver-backends.md): ``Simulation(backend="native")``; for problems the
environment overrides the spec, ``YAPNR_RF_BACKEND=native`` (``YAPNR_RF_DTYPE``, default
float64 for an overridden backend; ``YAPNR_RF_THREADS``, the native thread count). The library
is looked up at ``YAPNR_RF_FDTD_LIB``, then beside this module (``native/``), then in the Bazel
runfiles (``yapnr/rf/``). Without a loadable library (or on an ABI mismatch) the numpy reference
runs and `status` says why; a compiled dependency is never required. ``python -m
yapnr.rf.fdtd.native_kernel build`` compiles it with the host compiler into ``native/``.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import sys
from pathlib import Path

import numpy as np

from yapnr.rf.mesh import COMPONENTS, E_COMPONENTS

_ABI = 2
ENV_LIB = "YAPNR_RF_FDTD_LIB"
ENV_BACKEND = "YAPNR_RF_BACKEND"
ENV_DTYPE = "YAPNR_RF_DTYPE"
ENV_THREADS = "YAPNR_RF_THREADS"
ENV_TBLOCK = "YAPNR_RF_TBLOCK"
ENV_CACHE = "YAPNR_RF_CACHE_MB"
# Steps per wavefront pass: the planes a pass keeps (about 3 per step) should fit in this much
# cache (the last level a thread team shares), MiB.
DEFAULT_CACHE_MB = 8.0
MAX_TBLOCK = 8
BACKENDS = ("numpy", "torch", "native")

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "native" / "fdtd.c"
SOURCES = (SOURCE, HERE / "native" / "fdtd_kernels.h")
# No contraction into fused multiply-adds and no fast-math: every value as numpy computes it.
CFLAGS = [
    "-O3",
    "-std=c11",
    "-ffp-contract=off",
    "-fno-fast-math",
    "-fPIC",
    "-shared",
    "-pthread",
]
# Edges per work item of sources and probes (the C side's probe scratch holds 1024).
CHUNK = 512

_STATE = {"loaded": False, "kernel": None, "reason": "not requested", "warned": False}

c_int32, c_int64, c_void_p = ctypes.c_int32, ctypes.c_int64, ctypes.c_void_p


class YfTerm(ctypes.Structure):
    _fields_ = [
        ("src", c_int32),
        ("axis", c_int32),
        ("nslot", c_int32),
        ("pad_", c_int32),
        ("ik", c_void_p),
        ("slot", c_void_p),
        ("b", c_void_p),
        ("cd", c_void_p),
        ("psi", c_void_p),
    ]


class YfSeg(ctypes.Structure):
    _fields_ = [("j0", c_int32), ("j1", c_int32), ("yact", c_int32), ("slot0", c_int32)]


class YfComp(ctypes.Structure):
    _fields_ = [
        ("k0", c_int32),
        ("k1", c_int32),
        ("i0", c_int32),
        ("i1", c_int32),
        ("nseg", c_int32),
        ("pad_", c_int32),
        ("seg", c_void_p),
        ("t", YfTerm * 2),
    ]


class YfSim(ctypes.Structure):
    _fields_ = [
        ("nx", c_int32),
        ("ny", c_int32),
        ("nz", c_int32),
        ("ni", c_int32),
        ("jp", c_int32),
        ("np", c_int32),
        ("dsize", c_int32),
        ("kc", c_int32),
        ("sheet", c_int32),
        ("nmu", c_int32),
        ("f", c_void_p * 6),
        ("c", YfComp * 6),
        ("ca_s", c_void_p * 3),
        ("cb_s", c_void_p * 3),
        ("ca_t", c_void_p * 3),
        ("cb_t", c_void_p * 3),
        ("mu_comp", c_void_p),
        ("mu_k0", c_void_p),
        ("mu_k1", c_void_p),
        ("mu_m", c_void_p),
        ("mu_old", c_void_p),
        ("sh", (c_void_p * 9) * 2),
    ]


class YfSrc(ctypes.Structure):
    _fields_ = [
        ("comp", c_int32),
        ("count", c_int32),
        ("k", c_int32),
        ("pad_", c_int32),
        ("off", c_void_p),
        ("scale", c_void_p),
        ("amp", c_void_p),
        ("w", c_void_p),
        ("act", c_void_p),
        ("val", c_void_p),
    ]


class YfProbe(ctypes.Structure):
    _fields_ = [
        ("comp", c_int32),
        ("count", c_int32),
        ("off", c_void_p),
        ("re", c_void_p),
        ("im", c_void_p),
    ]


class YfItem(ctypes.Structure):
    _fields_ = [("index", c_int32), ("p0", c_int32), ("p1", c_int32), ("pad_", c_int32)]


class YfRun(ctypes.Structure):
    _fields_ = [
        ("n0", c_int64),
        ("n1", c_int64),
        ("dec", c_int32),
        ("m", c_int32),
        ("nsrc", c_int32),
        ("nprobe", c_int32),
        ("nhitem", c_int32),
        ("neitem", c_int32),
        ("nhsitem", c_int32),
        ("nesitem", c_int32),
        ("tblock", c_int32),
        ("nsitem", c_int32),
        ("src", c_void_p),
        ("probe", c_void_p),
        ("hitem", c_void_p),
        ("eitem", c_void_p),
        ("hsitem", c_void_p),
        ("esitem", c_void_p),
        ("sitem", c_void_p),
        ("hcos", c_void_p),
        ("hsin", c_void_p),
        ("ecos", c_void_p),
        ("esin", c_void_p),
        ("hs_ptr", c_void_p),
        ("es_ptr", c_void_p),
        ("hp_ptr", c_void_p),
        ("ep_ptr", c_void_p),
        ("hs_ent", c_void_p),
        ("es_ent", c_void_p),
        ("hp_ent", c_void_p),
        ("ep_ent", c_void_p),
    ]


_STRUCTS = (YfTerm, YfSeg, YfComp, YfSim, YfSrc, YfProbe, YfItem, YfRun)


# -- the library ---------------------------------------------------------------------------------


def library_names() -> list[str]:
    if sys.platform == "darwin":
        return ["libyapnr_fdtd.dylib", "libyapnr_fdtd.so"]
    return ["libyapnr_fdtd.so"]


def _candidates():
    configured = os.environ.get(ENV_LIB)
    if configured:
        yield Path(configured)
    # Beside the package (an installed or locally built library), then Bazel's runfiles
    # (yapnr/rf/, where //yapnr/rf:libyapnr_fdtd.so lands): the module's own (unresolved) path
    # is inside the runfiles tree, the resolved one is the source tree.
    roots = []
    for here in (Path(os.path.abspath(__file__)).parent, HERE):
        roots += [here / "native", here.parent]
    for env in ("RUNFILES_DIR", "TEST_SRCDIR"):
        if os.environ.get(env):
            roots.append(Path(os.environ[env]) / "_main" / "yapnr" / "rf")
    seen = set()
    for root in roots:
        for name in library_names():
            path = root / name
            if path not in seen:
                seen.add(path)
                yield path


class Kernel:
    """The loaded library."""

    def __init__(self, library, path: Path):
        self.lib = library
        self.path = Path(path)
        library.yf_abi.restype = c_int32
        library.yf_sizes.argtypes = [c_void_p]
        library.yf_isa.restype = ctypes.c_char_p
        library.yf_pool_new.restype = c_void_p
        library.yf_pool_new.argtypes = [c_int32]
        library.yf_pool_free.argtypes = [c_void_p]
        library.yf_pool_threads.restype = c_int32
        library.yf_pool_threads.argtypes = [c_void_p]
        library.yf_run_block.restype = c_int32
        library.yf_run_block.argtypes = [c_void_p, c_void_p, c_void_p]
        sizes = np.zeros(len(_STRUCTS), dtype=np.int64)
        library.yf_sizes(sizes.ctypes.data)
        want = [ctypes.sizeof(s) for s in _STRUCTS]
        if sizes.tolist() != want:
            raise OSError(f"structure sizes {sizes.tolist()} != {want}")
        self.isa = library.yf_isa().decode()
        self.sha256 = hashlib.sha256(self.path.read_bytes()).hexdigest()


def build_library(out_dir, compiler=None, timeout=300, extra_flags=()) -> Path:
    """Compile ``native/fdtd.c`` into ``out_dir`` with the host C compiler (``$CC`` or ``cc``)
    and return the library path. Bazel builds the same source with the same flags."""
    import shutil
    import subprocess

    compiler = compiler or os.environ.get("CC") or shutil.which("cc") or shutil.which("gcc")
    if not compiler:
        raise FileNotFoundError("no C compiler (set CC)")
    out = Path(out_dir) / library_names()[0]
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [compiler, *CFLAGS, *extra_flags, "-o", str(out), str(SOURCE)],
        check=True,
        timeout=timeout,
        capture_output=True,
    )
    return out


def reset() -> None:
    """Forget the loaded library (tests)."""
    _STATE.update(loaded=False, kernel=None, reason="not requested", warned=False)


def load() -> Kernel | None:
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
            if library.yf_abi() != _ABI:
                reasons.append(f"{path.name}: ABI mismatch")
                continue
            _STATE["kernel"] = Kernel(library, path)
            _STATE["reason"] = "loaded " + path.name
            return _STATE["kernel"]
        except (OSError, AttributeError) as error:
            reasons.append(f"{path.name}: {error}")
    _STATE["reason"] = "; ".join(reasons) or "library not found"
    return None


def require_or_warn() -> Kernel | None:
    """`load`, saying once on stderr why the numpy reference runs instead."""
    kernel = load()
    if kernel is None and not _STATE["warned"]:
        _STATE["warned"] = True
        sys.stderr.write(f"yapnr.rf native FDTD: {_STATE['reason']}; numpy backend used\n")
    return kernel


def status() -> dict:
    """The library behind backend "native", for provenance."""
    kernel = load()
    if kernel is None:
        return {"loaded": False, "reason": _STATE["reason"]}
    return {
        "loaded": True,
        "library": kernel.path.name,
        "sha256": kernel.sha256,
        "isa": kernel.isa,
    }


def provenance(sim) -> dict | None:
    """The native library and thread count of a simulation that runs on it, else None."""
    if getattr(sim, "_native", None) is None:
        return None
    out = status()
    out["threads"] = sim.threads
    return out


# -- backend selection ---------------------------------------------------------------------------


def choose(backend, dtype, spec_backend: str, spec_dtype, *, exact: bool = False):
    """(backend, dtype) of a problem: explicit arguments, then ``YAPNR_RF_BACKEND`` and
    ``YAPNR_RF_DTYPE``, then the spec. An exact problem runs numpy float64, or native float64
    (bit-identical) when the environment asks for native."""
    env_backend = os.environ.get(ENV_BACKEND, "").strip().lower() or None
    env_dtype = os.environ.get(ENV_DTYPE, "").strip().lower() or None
    if env_backend is not None and env_backend not in BACKENDS:
        raise ValueError(f"{ENV_BACKEND}={env_backend!r}: expected one of {BACKENDS}")
    if exact:
        chosen = backend or ("native" if env_backend == "native" else "numpy")
        return chosen, np.dtype(dtype or np.float64)
    chosen = backend or env_backend or spec_backend
    if dtype is not None:
        return chosen, np.dtype(dtype)
    if env_dtype is not None:
        return chosen, np.dtype({"f64": "float64", "f32": "float32"}.get(env_dtype, env_dtype))
    if backend is None and env_backend == "native" and spec_backend != "native":
        # The spec's precision was chosen for its own backend (torch float32 for speed); a
        # backend switched by the environment runs the reference precision unless asked.
        return chosen, np.dtype(np.float64)
    return chosen, np.dtype(spec_dtype)


def thread_count(threads: int) -> int:
    env = os.environ.get(ENV_THREADS, "").strip()
    n = int(env) if env else int(threads)
    return max(1, min(n, os.cpu_count() or 1))


# -- the stepper ---------------------------------------------------------------------------------


def _aligned_zeros(n: int, dtype, align: int = 64) -> np.ndarray:
    dtype = np.dtype(dtype)
    raw = np.zeros(n + align // dtype.itemsize, dtype=dtype)
    skip = (-raw.ctypes.data % align) // dtype.itemsize
    return raw[skip : skip + n]


def _ptr(a) -> int:
    return 0 if a is None else int(a.ctypes.data)


def _round_up(n: int, m: int) -> int:
    return -(-n // m) * m


class NativeStepper:
    """The native state of one `Simulation`: field buffers in the C layout (``sim.f`` views
    them), the CPML ψ arrays, coefficient tables and the thread pool."""

    def __init__(self, sim, kernel: Kernel, threads: int):
        self.sim = sim
        self.kernel = kernel
        grid = sim.grid
        self.dtype = np.dtype(sim.dtype)
        nx, ny, nz = grid.n
        self.ni, self.np_ = nx + 1, nz + 1
        self.jp = _round_up(ny + 1, 64 // self.dtype.itemsize)
        self.size = self.np_ * self.ni * self.jp
        self.bufs = {c: _aligned_zeros(self.size, self.dtype) for c in COMPONENTS}
        self.views = {c: self._view(self.bufs[c], grid.shape(c)) for c in COMPONENTS}
        self.threads = thread_count(threads)
        self.pool = None
        self._new_pool()
        self.s = YfSim()
        s = self.s
        s.nx, s.ny, s.nz = nx, ny, nz
        s.ni, s.jp, s.np = self.ni, self.jp, self.np_
        s.dsize = self.dtype.itemsize
        s.kc = grid.k_c
        for n, c in enumerate(COMPONENTS):
            s.f[n] = _ptr(self.bufs[c])
        self._keep = []  # arrays the C structures point to
        self._psi = []
        self._build_comps()
        self._mat_keep = []
        self.mu_old, self.sheet_state = [], []
        self.tblock = self._tblock()

    def _new_pool(self) -> None:
        self.pool = self.kernel.lib.yf_pool_new(self.threads)
        if not self.pool:
            raise MemoryError("native FDTD thread pool")
        self.pool_pid = os.getpid()
        self.threads = int(self.kernel.lib.yf_pool_threads(self.pool))

    def pool_handle(self) -> int:
        """The thread pool; a new one in a forked child (which inherits the handle but not the
        threads; the parent's pool is left alone)."""
        if self.pool_pid != os.getpid():
            self._new_pool()
        return self.pool

    def __del__(self):
        pool = getattr(self, "pool", None)
        if pool and getattr(self, "pool_pid", None) == os.getpid():
            try:
                self.kernel.lib.yf_pool_free(pool)
            except Exception:  # interpreter shutdown
                pass
            self.pool = None

    def _tblock(self) -> int:
        """Steps per wavefront pass, ``$YAPNR_RF_TBLOCK``: unset or 0, the sweeps (one pass over
        the box per half step); N, N steps per pass; "auto", as many as keep about three planes
        per step within ``$YAPNR_RF_CACHE_MB`` (0 when not even one fits)."""
        env = os.environ.get(ENV_TBLOCK, "").strip().lower()
        if env != "auto":
            return max(0, min(MAX_TBLOCK, int(env or 0)))
        cache = float(os.environ.get(ENV_CACHE, "").strip() or DEFAULT_CACHE_MB) * 2**20
        plane = 6 * self.ni * self.jp * self.dtype.itemsize
        plane += sum(p.nbytes for p in self._psi) / self.np_
        return max(0, min(MAX_TBLOCK, int(cache // (3 * plane))))

    # -- layout ----------------------------------------------------------------------------------

    def _view(self, buf: np.ndarray, shape) -> np.ndarray:
        sx, sy, sz = shape
        return buf.reshape(self.np_, self.ni, self.jp)[:sz, :sx, :sy].transpose(1, 2, 0)

    def offsets(self, comp: str, flat) -> np.ndarray:
        """Offsets into the C buffer of `comp` of flat (C-order) indices of its array."""
        i, j, k = np.unravel_index(np.asarray(flat, dtype=np.int64), self.sim.grid.shape(comp))
        return np.ascontiguousarray((k * self.ni + i) * self.jp + j, dtype=np.int64)

    def _region(self, comp: str):
        """Native ranges (x, y, z) of the updated samples of `comp`."""
        shape = self.sim.grid.shape(comp)
        if comp[0] == "h":
            return [(0, n) for n in shape]
        sl = self.sim._interior[comp]
        return [tuple(sl[a].indices(shape[a])[:2]) for a in range(3)]

    def _build_comps(self) -> None:
        sim, s, dt = self.sim, self.s, self.dtype
        grid = sim.grid
        index = {c: n for n, c in enumerate(COMPONENTS)}
        for n, comp in enumerate(COMPONENTS):
            electric = comp[0] == "e"
            (i0, i1), (j0, j1), (k0, k1) = self._region(comp)
            C = s.c[n]
            C.k0, C.k1, C.i0, C.i1 = k0, k1, i0, i1
            terms = sim.terms[comp]
            if [t.sign for t in terms] != [1, -1]:
                raise ValueError("the native kernel expects curl terms (+, -)")
            yruns = []
            for m, t in enumerate(terms):
                u = t.axis
                n_u = grid.axis(u).n
                shift = 1 if electric else 0  # E: derivative index m is node m + 1
                ik = np.zeros(n_u + 1, dtype=dt)
                ik1 = np.asarray(t.ik, dtype=dt).reshape(-1)
                ik[shift : shift + ik1.size] = ik1
                slot = np.full(n_u + 1, -1, dtype=np.int32)
                bs, cds, runs = [], [], []
                for sl in t.slabs:
                    a0, a1 = sl.sl[u].start, sl.sl[u].stop
                    b = np.asarray(sl.b, dtype=dt)
                    cd = np.asarray(sl.cd, dtype=dt)
                    if sl.gap:
                        parts = [
                            (a0, a1, b.reshape(2, -1)[0], cd.reshape(2, -1)[0]),
                            (a0 + sl.gap, a1 + sl.gap, b.reshape(2, -1)[1], cd.reshape(2, -1)[1]),
                        ]
                    else:
                        parts = [(a0, a1, b.reshape(-1), cd.reshape(-1))]
                    for p0, p1, pb, pcd in parts:
                        runs.append((p0 + shift, p1 + shift, pb, pcd))
                runs.sort(key=lambda r: r[0])
                nslot = 0
                for p0, p1, pb, pcd in runs:
                    slot[p0:p1] = np.arange(nslot, nslot + (p1 - p0), dtype=np.int32)
                    bs.append(pb)
                    cds.append(pcd)
                    nslot += p1 - p0
                b_all = np.ascontiguousarray(np.concatenate(bs) if bs else np.zeros(1), dtype=dt)
                cd_all = np.ascontiguousarray(np.concatenate(cds) if cds else np.zeros(1), dtype=dt)
                T = C.t[m]
                T.src = index[t.src]
                T.axis = u
                psi = None
                if nslot:
                    if u == 0:
                        psi = _aligned_zeros(self.np_ * nslot * self.jp, dt)
                        T.nslot = nslot
                    elif u == 2:
                        psi = _aligned_zeros(nslot * self.ni * self.jp, dt)
                        T.nslot = nslot
                    else:
                        stride = _round_up(nslot, 64 // dt.itemsize)
                        psi = _aligned_zeros(self.np_ * self.ni * stride, dt)
                        T.nslot = stride
                        yruns = [(p0, p1, int(slot[p0])) for p0, p1, _, _ in runs]
                else:
                    T.nslot = 0
                T.ik, T.slot, T.b, T.cd, T.psi = (
                    _ptr(ik),
                    _ptr(slot),
                    _ptr(b_all),
                    _ptr(cd_all),
                    _ptr(psi),
                )
                self._keep += [ik, slot, b_all, cd_all]
                if psi is not None:
                    self._psi.append(psi)
            segs = []
            cuts = sorted({j0, j1, *[max(j0, min(j1, x)) for r in yruns for x in r[:2]]})
            for a, b in zip(cuts[:-1], cuts[1:]):
                if b <= a:
                    continue
                act, slot0 = 0, 0
                for p0, p1, s0 in yruns:
                    if p0 <= a and b <= p1:
                        act, slot0 = 1, s0 + (a - p0)
                segs.append((a, b, act, slot0))
            seg = np.array(segs, dtype=np.int32).reshape(-1, 4)
            self._keep.append(seg)
            C.nseg = len(segs)
            C.seg = _ptr(seg)

    # -- materials -------------------------------------------------------------------------------

    def _plane_table(self, comp: str, plane: np.ndarray, i0: int, j0: int) -> np.ndarray:
        tab = _aligned_zeros(self.ni * self.jp, self.dtype).reshape(self.ni, self.jp)
        tab[i0 : i0 + plane.shape[0], j0 : j0 + plane.shape[1]] = plane
        return tab

    def set_materials(self, ca: dict, cb: dict, mu: list, sheet: dict) -> None:
        """Coefficients from `Simulation.update_materials`: interior Ca/Cb arrays (the dtype's
        values), the μ entries and the sheet's branch arrays."""
        s, dt = self.s, self.dtype
        keep = []
        for n, comp in enumerate(E_COMPONENTS):
            (i0, _), (j0, _), (k0, k1) = self._region(comp)
            a = np.asarray(ca[comp], dtype=dt)
            b = np.asarray(cb[comp], dtype=dt)
            sa = np.zeros(self.np_, dtype=dt)
            sb = np.zeros(self.np_, dtype=dt)
            ta = (c_void_p * self.np_)()
            tb = (c_void_p * self.np_)()
            for k in range(k0, k1):
                pa, pb = a[:, :, k - k0], b[:, :, k - k0]
                if pa.size and np.all(pa == pa.flat[0]) and np.all(pb == pb.flat[0]):
                    sa[k], sb[k] = pa.flat[0], pb.flat[0]
                else:
                    xa = self._plane_table(comp, pa, i0, j0)
                    xb = self._plane_table(comp, pb, i0, j0)
                    ta[k], tb[k] = _ptr(xa), _ptr(xb)
                    keep += [xa, xb]
            s.ca_s[n], s.cb_s[n] = _ptr(sa), _ptr(sb)
            s.ca_t[n] = ctypes.addressof(ta)
            s.cb_t[n] = ctypes.addressof(tb)
            keep += [sa, sb, ta, tb]
        index = {c: n for n, c in enumerate(COMPONENTS)}
        s.nmu = len(mu)
        self.mu_old = []
        if mu:
            comps = np.array([index[c] for c, *_ in mu], dtype=np.int32)
            k0s = np.array([k0 for _, k0, _, _ in mu], dtype=np.int32)
            k1s = np.array([k1 for _, _, k1, _ in mu], dtype=np.int32)
            ms = (c_void_p * len(mu))()
            olds = (c_void_p * len(mu))()
            for e, (comp, k0, k1, m) in enumerate(mu):
                m = np.asarray(m, dtype=dt)
                tab = _aligned_zeros((k1 - k0) * self.ni * self.jp, dt).reshape(
                    k1 - k0, self.ni, self.jp
                )
                for p in range(k1 - k0):
                    tab[p, : m.shape[0], : m.shape[1]] = m[:, :, p]
                old = _aligned_zeros(tab.size, dt)
                ms[e], olds[e] = _ptr(tab), _ptr(old)
                keep += [tab]
                self.mu_old.append(old)
            s.mu_comp, s.mu_k0, s.mu_k1 = _ptr(comps), _ptr(k0s), _ptr(k1s)
            s.mu_m, s.mu_old = ctypes.addressof(ms), ctypes.addressof(olds)
            keep += [comps, k0s, k1s, ms, olds]
        else:
            s.mu_comp = s.mu_k0 = s.mu_k1 = s.mu_m = s.mu_old = 0
        s.sheet = 1 if sheet else 0
        self.sheet_state = []
        for n, comp in enumerate(("ex", "ey")):
            for q in range(9):
                s.sh[n][q] = 0
            if not sheet:
                continue
            (i0, _), (j0, _), _ = self._region(comp)
            arrs = sheet[comp]
            names = [("c1", 0), ("c1", 1), ("k", 0), ("k", 1), ("bh", 0), ("bh", 1)]
            for q, (key, side) in enumerate(names):
                tab = self._plane_table(comp, np.asarray(arrs[key][side], dtype=dt), i0, j0)
                s.sh[n][q] = _ptr(tab)
                keep.append(tab)
            for q in (6, 7, 8):
                st = _aligned_zeros(self.ni * self.jp, dt)
                s.sh[n][q] = _ptr(st)
                self.sheet_state.append(st)
        self._mat_keep = keep

    def reset(self) -> None:
        for b in self.bufs.values():
            b.fill(0)
        for p in self._psi:
            p.fill(0)
        for a in self.mu_old + self.sheet_state:
            a.fill(0)

    # -- running ---------------------------------------------------------------------------------

    def start(self, srcs, probes, omega_count: int, decimation: int) -> "_NativeRun":
        return _NativeRun(self, srcs, probes, omega_count, decimation)


def _items(counts, magnetic_flags, want_magnetic: bool) -> np.ndarray:
    rows = []
    for n, (count, mag) in enumerate(zip(counts, magnetic_flags)):
        if mag != want_magnetic:
            continue
        for p0 in range(0, count, CHUNK):
            rows.append((n, p0, min(count, p0 + CHUNK), 0))
    return np.array(rows, dtype=np.int32).reshape(-1, 4)


def _row_lists(stepper, members, rows: int):
    """CSR of edges by row of the box: ptr [rows + 1] (int64) and entries {index, p} (int32),
    in the members' order (list order, then edge order) within each row."""
    if not members:
        return np.zeros(rows + 1, dtype=np.int64), np.zeros((1, 4), dtype=np.int32)
    row, ent = [], []
    for n, comp, idx in members:
        off = stepper.offsets(comp, idx)
        row.append(off // stepper.jp)
        e = np.zeros((idx.size, 4), dtype=np.int32)
        e[:, 0] = n
        e[:, 1] = np.arange(idx.size)
        ent.append(e)
    row = np.concatenate(row)
    ent = np.concatenate(ent)
    order = np.argsort(row, kind="stable")
    ptr = np.zeros(rows + 1, dtype=np.int64)
    ptr[1:] = np.cumsum(np.bincount(row, minlength=rows))
    return ptr, np.ascontiguousarray(ent[order])


class _NativeRun:
    """The per-run C structures: sources and probes with their offsets and buffers."""

    def __init__(self, stepper: NativeStepper, srcs, probes, omega_count: int, decimation: int):
        self.st = stepper
        tblock = stepper.tblock
        index = {c: n for n, c in enumerate(COMPONENTS)}
        self.srcs = srcs
        self.src = (YfSrc * max(1, len(srcs)))()
        self.keep = []
        for n, sp in enumerate(srcs):
            q = self.src[n]
            q.comp = index[sp.comp]
            q.count = sp.index.size
            off = stepper.offsets(sp.comp, sp.index)
            scale = np.ascontiguousarray(sp.scale, dtype=np.float64)
            val = np.zeros(max(1, tblock) * max(1, sp.index.size))
            q.off, q.scale, q.val = _ptr(off), _ptr(scale), _ptr(val)
            if sp.amp is not None:
                amp = np.ascontiguousarray(sp.amp, dtype=np.float64)
                q.k = amp.shape[0]
                q.amp = _ptr(amp)
                self.keep.append(amp)
            else:
                q.k = 0
                q.amp = 0
            self.keep += [off, scale, val]
        self.probe = (YfProbe * max(1, len(probes)))()
        for n, (p, acc) in enumerate(probes):
            q = self.probe[n]
            q.comp = index[p.comp]
            q.count = p.index.size
            off = stepper.offsets(p.comp, p.index)
            if not (acc.re.flags.c_contiguous and acc.im.flags.c_contiguous):
                raise ValueError("DTFT accumulators must be C-contiguous")
            q.off, q.re, q.im = _ptr(off), _ptr(acc.re), _ptr(acc.im)
            self.keep.append(off)
        pmag = [p.comp[0] == "h" for p, _ in probes]
        smag = [sp.comp[0] == "h" for sp in srcs]
        self.hitem = _items([p.index.size for p, _ in probes], pmag, True)
        self.eitem = _items([p.index.size for p, _ in probes], pmag, False)
        self.hsitem = _items([sp.index.size for sp in srcs], smag, True)
        self.esitem = _items([sp.index.size for sp in srcs], smag, False)
        self.sitem = np.concatenate([self.hsitem, self.esitem])
        r = self.run = YfRun()
        r.tblock = tblock
        r.nsitem = len(self.sitem)
        r.sitem = _ptr(self.sitem)
        if tblock:
            # Per-row lists of the source and probe edges (CSR over the box's rows).
            rows = stepper.np_ * stepper.ni
            groups = {
                "hs": [(n, sp.comp, sp.index) for n, sp in enumerate(srcs) if smag[n]],
                "es": [(n, sp.comp, sp.index) for n, sp in enumerate(srcs) if not smag[n]],
                "hp": [(n, p.comp, p.index) for n, (p, _) in enumerate(probes) if pmag[n]],
                "ep": [(n, p.comp, p.index) for n, (p, _) in enumerate(probes) if not pmag[n]],
            }
            for key, members in groups.items():
                ptr, ent = _row_lists(stepper, members, rows)
                self.keep += [ptr, ent]
                setattr(r, key + "_ptr", _ptr(ptr) if members else 0)
                setattr(r, key + "_ent", _ptr(ent) if members else 0)
        r.dec, r.m = int(decimation), int(omega_count)
        r.nsrc, r.nprobe = len(srcs), len(probes)
        r.nhitem, r.neitem = len(self.hitem), len(self.eitem)
        r.nhsitem, r.nesitem = len(self.hsitem), len(self.esitem)
        r.src, r.probe = ctypes.addressof(self.src), ctypes.addressof(self.probe)
        r.hitem, r.eitem = _ptr(self.hitem), _ptr(self.eitem)
        r.hsitem, r.esitem = _ptr(self.hsitem), _ptr(self.esitem)

    def block(self, n0: int, n1: int, tables) -> None:
        """Run steps n0 .. n1 − 1 with the block's tables (`engine._BlockTables`)."""
        r = self.run
        r.n0, r.n1 = n0, n1
        keep = []
        for n, (w, act) in enumerate(tables.sources):
            w = np.ascontiguousarray(w, dtype=np.float64)
            act = np.ascontiguousarray(act, dtype=np.uint8)
            self.src[n].w, self.src[n].act = _ptr(w), _ptr(act)
            keep += [w, act]
        hc, hs, ec, es = (np.ascontiguousarray(a, dtype=np.float64) for a in tables.phases)
        r.hcos, r.hsin, r.ecos, r.esin = _ptr(hc), _ptr(hs), _ptr(ec), _ptr(es)
        st = self.st
        err = st.kernel.lib.yf_run_block(
            c_void_p(st.pool_handle()), ctypes.addressof(st.s), ctypes.addressof(r)
        )
        del keep
        if err:
            raise RuntimeError(f"native FDTD block failed ({err})")


def main(argv=None) -> int:
    import argparse

    ap = argparse.ArgumentParser(prog="python -m yapnr.rf.fdtd.native_kernel")
    sub = ap.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build", help="compile the library with the host C compiler")
    b.add_argument("--out", default=str(HERE / "native"), help="directory (default: native/)")
    sub.add_parser("status", help="say which library backend 'native' loads")
    args = ap.parse_args(argv)
    if args.cmd == "build":
        print(build_library(args.out))
        return 0
    print(status())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
