#!/usr/bin/env python3
"""What an image offers ``yapnr.rf``: a diagnostic task that writes ``OUT/diag.json``.

    python job/rf_diag.py --out out/diag [--src src]

Records the interpreter, the CPUs the task sees, the versions of numpy, torch, Pillow, PyYAML
(or the import error), torch's threading, a short float32 matmul timing, whether every module of
``SRC/yapnr/rf`` compiles on this interpreter, which ``yapnr`` package is imported (the source
bundle's, not the image's), the native FDTD library the bundle's sources would run (``native``:
the loader's status, which names the image's wheel library when it was built from the bundle's
C sources, or why not), and the exit code and head of ``python -m yapnr.rf.cases --help``.
``ok`` is true when numpy, torch and PyYAML import, the sources compile, ``yapnr.rf.cases``
comes from the bundle and its help exits 0; Pillow (the animations) and the native library are
reported, not required, except that ``YAPNR_RF_REQUIRE_NATIVE=1`` (which ``rf_stage_plan``
sets by default) makes ``ok`` require the native library.

Written by tools/exp/rf_stage_plan.py into the job bundle of an ``mc-eval`` stage plan.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

REQUIRED = ("numpy", "torch", "yaml")
OPTIONAL = ("PIL",)
HELP_TIMEOUT_S = 300
NOT_SET = ("", "0", "false", "no")  # YAPNR_RF_REQUIRE_NATIVE values that do not require it


def _module(name):
    try:
        module = importlib.import_module(name)
    except Exception as err:  # noqa: BLE001 - any import failure is the finding
        return {"ok": False, "error": "%s: %s" % (type(err).__name__, err)}
    return {"ok": True, "version": getattr(module, "__version__", None)}


def _torch():
    import torch

    info = {
        "num_threads": torch.get_num_threads(),
        "num_interop_threads": torch.get_num_interop_threads(),
        "mkldnn": bool(torch.backends.mkldnn.is_available()),
        "openmp": bool(torch.backends.openmp.is_available()),
        "parallel_info": torch.__config__.parallel_info().splitlines()[:8],
    }
    a = torch.rand(1024, 1024, dtype=torch.float32)
    t0 = time.perf_counter()
    for _ in range(20):
        a = (a @ a).clamp_(-1, 1)
    info["matmul_1024_x20_s"] = round(time.perf_counter() - t0, 4)
    return info


def _compiles(src):
    failures = []
    files = sorted((src / "yapnr" / "rf").rglob("*.py"))
    for path in files:
        try:
            compile(path.read_bytes(), str(path), "exec", dont_inherit=True)
        except (SyntaxError, ValueError) as err:
            failures.append({"file": str(path.relative_to(src)), "error": str(err)[-300:]})
    return {"files": len(files), "failures": failures}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="rf_diag.py", description=__doc__.split("\n")[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--src", default="src", help="the source bundle's directory")
    args = ap.parse_args(argv)
    out, src = Path(args.out), Path(args.src)
    out.mkdir(parents=True, exist_ok=True)

    report = {
        "python": platform.python_version(),
        "executable": sys.executable,
        "platform": "%s-%s" % (sys.platform, platform.machine()),
        "cpu_count": os.cpu_count(),
        "affinity": len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else None,
        "env": {
            k: os.environ.get(k)
            for k in ("OMP_NUM_THREADS", "MKL_NUM_THREADS", "OPENBLAS_NUM_THREADS", "PYTHONPATH")
        },
        "modules": {name: _module(name) for name in REQUIRED + OPTIONAL},
    }
    if report["modules"]["torch"]["ok"]:
        try:
            report["torch"] = _torch()
        except Exception as err:  # noqa: BLE001
            report["torch"] = {"error": "%s: %s" % (type(err).__name__, err)}
    report["compile"] = _compiles(src)
    try:
        import yapnr
        from yapnr.rf import cases

        report["yapnr_file"] = str(Path(yapnr.__file__).resolve())
        report["from_bundle"] = Path(yapnr.__file__).resolve().is_relative_to(src.resolve())
        report["cases"] = sorted(cases.CASES)
    except Exception as err:  # noqa: BLE001
        report["yapnr_error"] = "%s: %s" % (type(err).__name__, err)
        report["from_bundle"] = False
    try:
        from yapnr.rf.fdtd import native_kernel

        report["native"] = native_kernel.status()
    except Exception as err:  # noqa: BLE001 - a bundle from before the native kernel
        report["native"] = {"loaded": False, "reason": "%s: %s" % (type(err).__name__, err)}
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "yapnr.rf.cases", "--help"],
            capture_output=True,
            text=True,
            timeout=HELP_TIMEOUT_S,
        )
        report["cases_help"] = {
            "exit_code": proc.returncode,
            "head": (proc.stdout or proc.stderr).splitlines()[:6],
        }
    except (OSError, subprocess.SubprocessError) as err:
        report["cases_help"] = {"exit_code": None, "error": str(err)}
    # With YAPNR_RF_REQUIRE_NATIVE (rf_stage_plan sets it unless a job opts out), a library the
    # loader refuses fails the diagnostic, as it would fail the campaign's runs.
    required = os.environ.get("YAPNR_RF_REQUIRE_NATIVE", "").strip().lower() not in NOT_SET
    report["native_required"] = required
    report["ok"] = bool(
        all(report["modules"][name]["ok"] for name in REQUIRED)
        and not report["compile"]["failures"]
        and report["compile"]["files"] > 0
        and report.get("from_bundle")
        and report["cases_help"].get("exit_code") == 0
        and (report["native"].get("loaded") or not required)
    )
    (out / "diag.json").write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    print(json.dumps({"ok": report["ok"]}))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
