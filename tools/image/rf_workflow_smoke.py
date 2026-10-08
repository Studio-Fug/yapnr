"""Run a bounded RF design/export/revalidation using only the installed package.

This tiny-grid packaging check makes no claim of RF performance qualification.
"""

from __future__ import annotations

import os
import tempfile
from dataclasses import replace
from pathlib import Path


def main() -> None:
    # Set BLAS caps before importing NumPy/Torch, not after pool creation.
    for name in (
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
        "YAPNR_RF_THREADS",
    ):
        os.environ[name] = "1"
    import torch

    # Tiny smoke workloads otherwise oversubscribe shared CI/Bazel workers.
    os.environ["YAPNR_RF_THREADS"] = "1"
    torch.set_num_threads(1)
    torch.set_num_interop_threads(1)
    from yapnr.rf.cases import divider
    from yapnr.rf.driver import design
    from yapnr.rf.export.kicad import read_footprint
    from yapnr.rf.export.touchstone import read_touchstone, write_touchstone
    from yapnr.rf.validate import resimulate

    spec = divider("smoke")
    spec = spec.replace(
        optimizer=replace(spec.optimizer, betas=(1.0,), iterations_per_beta=1, min_iterations=1)
    )
    with tempfile.TemporaryDirectory(prefix="rf-installed-smoke-") as root:
        design(spec, root, resume=False, budget_s=30, sweep=False, log=lambda *_: None)
        assert read_footprint(str(Path(root) / "footprint.kicad_mod"))
        check = resimulate(root, refine=1, backend="native", log=lambda *_: None)
        assert check["s"].shape[1:] == (3, 3)
        output = str(Path(root) / "validation.s3p")
        write_touchstone(output, check["freqs"], check["s"], z_ref=50)
        _, s, z0 = read_touchstone(output)
        assert s.shape[1:] == (3, 3) and z0 == 50
    print("installed RF design, footprint export, Touchstone and revalidation passed")


if __name__ == "__main__":
    main()
