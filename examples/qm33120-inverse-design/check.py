"""Run bounded, local-only publication checks without dispatching native FDTD."""

import os
import subprocess
import sys
from pathlib import Path


def main():
    root = Path(__file__).resolve().parent
    env = dict(os.environ)
    for key in (
        "OMP_NUM_THREADS",
        "OPENBLAS_NUM_THREADS",
        "MKL_NUM_THREADS",
        "VECLIB_MAXIMUM_THREADS",
    ):
        env[key] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root), str(root / "tests"), str(root.parents[1]), env.get("PYTHONPATH", "")]
    )
    for args in (
        ("-m", "unittest", "discover", "-s", "tests", "-v"),
        ("tests/test_acceptance_controls.py",),
    ):
        subprocess.run([sys.executable, *args], cwd=root, env=env, timeout=120, check=True)
    print("Offline checks passed; no native FDTD, cloud calls, or optimizer updates.")


if __name__ == "__main__":
    main()
