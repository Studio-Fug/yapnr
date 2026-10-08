"""Smoke check of yapnr.rf's native FDTD library in the yapnr image (tools/image/smoke_image.sh).

Run by the image's controller Python, with the installed yapnr wheel:

- the library loads (``YAPNR_RF_REQUIRE_NATIVE``: no fallback) from the wheel, beside the
  installed package, and passes the loader's checks (ABI, no fused or reassociated
  arithmetic, built from the wheel's sources);
- the default backend is native;
- 40 steps of random fields on a small microstrip grid with the copper-edge correction equal
  the numpy reference bit for bit, on one and on two threads.

Prints one line: the library, its instruction set and compiler.
"""

from __future__ import annotations

import os
import sys

import numpy as np

os.environ["YAPNR_RF_REQUIRE_NATIVE"] = "1"

from yapnr.rf.domain import Domain, DomainSpec, PortSpec  # noqa: E402
from yapnr.rf.fdtd import native_kernel  # noqa: E402
from yapnr.rf.fdtd.engine import Simulation  # noqa: E402
from yapnr.rf.mesh import COMPONENTS  # noqa: E402
from yapnr.rf.stackup import Stackup  # noqa: E402


def main() -> int:
    # Public external-project workflow imports: Bazel's solver closure used to
    # hide omitted RF subpackages in the installed wheel (#96).
    from yapnr.rf.driver import design
    from yapnr.rf.export import contour, drc, raster, repair, report
    from yapnr.rf.export.kicad import write_footprint
    from yapnr.rf.export.touchstone import write_touchstone
    from yapnr.rf.palace.schema import load
    from yapnr.rf.planar.adapters import read_kicad_copper
    from yapnr.rf.validate import resimulate

    assert all(
        callable(fn)
        for fn in (design, resimulate, write_footprint, write_touchstone, read_kicad_copper)
    )
    assert all(module.__file__ for module in (contour, drc, raster, repair, report))
    assert load() is not None, "installed Palace schema missing"
    kernel = native_kernel.load()
    status = native_kernel.status()
    if kernel is None:
        print("native FDTD library not loaded: " + status["reason"], file=sys.stderr)
        return 1
    import yapnr

    package = os.path.dirname(os.path.abspath(yapnr.__file__))
    if not str(kernel.path).startswith(package):
        print(f"library {kernel.path} is not the installed package's", file=sys.stderr)
        return 1
    if kernel.src_sha != native_kernel.source_sha256():
        print("the library does not record the package's sources", file=sys.stderr)
        return 1
    spec = DomainSpec(
        Stackup(3.55, 0.0027, 0.813e-3, 10e9),
        0.4e-3,
        2,
        (0.0, 3.2e-3, -1.6e-3, 1.6e-3),
        (PortSpec(1, "W", 0.0, 4), PortSpec(2, "E", 0.0, 4)),
        f_max=14e9,
        meas_cells=3,
        src_cells=7,
        pml_gap=2,
        margin=1.6e-3,
        air=3.0e-3,
        n_pml=6,
        n_pml_top=6,
    )
    dom = Domain(spec)
    rng = np.random.default_rng(5)
    stack = dom.spec.stackup
    g = stack.g_min * (stack.g_max / stack.g_min) ** rng.uniform(0, 1, dom.design_shape)
    copper = rng.uniform(0, 1, dom.design_shape)
    dt = 0.9 * dom.grid.courant_dt()
    start = {c: rng.standard_normal(dom.grid.shape(c)) for c in COMPONENTS}
    fields = {}
    for backend, threads in (("numpy", 1), (None, 1), (None, 2)):
        sim = Simulation(
            dom.grid, dom.structure(g, copper=copper), dt=dt, backend=backend, threads=threads
        )
        if backend is None and sim.backend != "native":
            print(f"the default backend is {sim.backend}, not native", file=sys.stderr)
            return 1
        for c in COMPONENTS:
            sim.f[c][...] = start[c]
        sim.advance(40)
        fields[(sim.backend, threads)] = {c: sim.field(c) for c in COMPONENTS}
    ref = fields[("numpy", 1)]
    for key, got in fields.items():
        for c in COMPONENTS:
            if not np.array_equal(got[c], ref[c]):
                print(f"{key}: {c} differs from numpy", file=sys.stderr)
                return 1
    print(f"{os.path.basename(str(kernel.path))} ({status['isa']}; {status['compiler']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
