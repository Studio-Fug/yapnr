#!/usr/bin/env python3
"""Assemble the openEMS runs of `order0_em.py` into S-matrices and compare them with yapnr.rf.

    python3 post.py RUNS_DIR --out DIR

RUNS_DIR holds one directory per run (``<model>-p<n>``, each with ``result.json``). For every
model the S-matrix is S = B A⁻¹ from the waves of all its excitations; a run that was not made
is taken from the model's symmetry (SYMMETRY: the port permutation that maps the model onto
itself, D1/R1 mirror P2 and P3, the lines and sticks swap P1 and P2). The S-matrix is then
renormalized from each port's own line impedance (its real part, as yapnr.rf does) to 50 Ω and
written as Touchstone (engineering convention, e^{+jωt}).

Standard library and numpy only (it runs on the Mac or in the openEMS image).
"""

from __future__ import annotations

import argparse
import json
import math
import os

import numpy as np

SYMMETRY = {
    "d1": {1: 1, 2: 3, 3: 2},
    "r1": {1: 1, 2: 3, 3: 2},
    "line": {1: 2, 2: 1},
    "a01": {1: 2, 2: 1},
    "a04": {1: 2, 2: 1},
}


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        r = json.load(fh)
    f = np.asarray(r["f_hz"])
    ports = {}
    for n, d in r["ports"].items():
        ports[int(n)] = {k: np.asarray(v[0]) + 1j * np.asarray(v[1]) for k, v in d.items()}
    return dict(f=f, excite=int(r["excite"]), ports=ports, raw=r)


def family(model: str) -> str:
    for k in SYMMETRY:
        if model == k or model.startswith(k + "-") or model.startswith(k + "_"):
            return k
    if model.startswith("line"):
        return "line"
    raise KeyError(model)


def assemble(runs: list, model: str) -> dict:
    """S (F, N, N) referenced to each port's own Z, the ports' Z (F, N) and the frequencies."""
    perm = SYMMETRY[family(model)]
    by_exc = {r["excite"]: r for r in runs}
    n = len(runs[0]["ports"])
    f = runs[0]["f"]
    A = np.zeros((f.size, n, n), complex)
    B = np.zeros((f.size, n, n), complex)
    Z = np.zeros((f.size, n), complex)
    for j in range(1, n + 1):
        if j in by_exc:
            r, mp = by_exc[j], {k: k for k in range(1, n + 1)}
        else:
            src = perm[j]
            if src not in by_exc:
                raise ValueError(f"{model}: neither excitation {j} nor its mirror {src} was run")
            r, mp = by_exc[src], perm  # port k of the mirrored run is port perm[k] of the real one
        for k in range(1, n + 1):
            p = r["ports"][mp[k]]
            A[:, k - 1, j - 1] = p["a"]
            B[:, k - 1, j - 1] = p["b"]
    for k in range(1, n + 1):
        zs = [r["ports"][k]["z"] for r in runs]
        Z[:, k - 1] = np.mean(zs, axis=0)
    S = B @ np.linalg.inv(A)
    return dict(f=f, s=S, z=Z)


def renormalize(s: np.ndarray, zc: np.ndarray, z_ref: float = 50.0) -> np.ndarray:
    """yapnr.rf.sparams.renormalize: per-port real Zc to a common real z_ref."""
    zc = np.real(zc)
    n = s.shape[-1]
    eye = np.eye(n)
    sq = np.sqrt(zc)[..., :, None] * eye
    z = sq @ (eye + s) @ np.linalg.inv(eye - s) @ sq
    return (z - z_ref * eye) @ np.linalg.inv(z + z_ref * eye)


def line_loss_db(s: np.ndarray) -> np.ndarray:
    s11, s12, s21, s22 = s[:, 0, 0], s[:, 0, 1], s[:, 1, 0], s[:, 1, 1]
    a = ((1 + s11) * (1 - s22) + s12 * s21) / (2 * s21)
    return 20 * np.log10(math.e) * np.abs(np.real(np.arccosh(a.astype(complex))))


def write_touchstone(path: str, f: np.ndarray, s: np.ndarray, comments=()) -> None:
    n = s.shape[-1]
    with open(path, "w", encoding="utf-8") as fh:
        for c in comments:
            fh.write(f"! {c}\n")
        fh.write("# HZ S RI R 50\n")
        for k in range(f.size):
            vals = []
            if n == 2:  # Touchstone 1: 2-ports in S11 S21 S12 S22 order
                order = [(0, 0), (1, 0), (0, 1), (1, 1)]
            else:
                order = [(i, j) for i in range(n) for j in range(n)]
            for i, j in order:
                vals += [f"{s[k, i, j].real:.9e}", f"{s[k, i, j].imag:.9e}"]
            if n <= 2:
                fh.write(f"{f[k]:.6e} " + " ".join(vals) + "\n")
            else:
                for row in range(n):
                    chunk = vals[2 * n * row : 2 * n * (row + 1)]
                    lead = f"{f[k]:.6e} " if row == 0 else " " * 13
                    fh.write(lead + " ".join(chunk) + "\n")


def collect(runs_dir: str) -> dict:
    """{model: [runs]} from RUNS_DIR/<model>-p<n>/result.json."""
    out: dict = {}
    for d in sorted(os.listdir(runs_dir)):
        p = os.path.join(runs_dir, d, "result.json")
        if not os.path.isfile(p):
            continue
        r = load(p)
        model = r["raw"]["model"]
        if r["raw"].get("kind") == "msl":  # one model at several meshes: d1-r05, d1-r025
            model += "-r" + ("%g" % r["raw"]["res_mm"]).replace("0.", "")
        out.setdefault(model, []).append(r)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("runs")
    ap.add_argument("--out", required=True)
    a = ap.parse_args(argv)
    os.makedirs(a.out, exist_ok=True)
    summary = {}
    for model, runs in collect(a.runs).items():
        res = assemble(runs, model)
        if runs[0]["raw"].get("kind") == "launch":
            s50 = res["s"]  # coax TEM ports: already referenced to the coax (about 50 ohm)
        else:
            s50 = renormalize(res["s"], res["z"])
        n = s50.shape[-1]
        path = os.path.join(a.out, f"{model}.s{n}p")
        write_touchstone(
            path,
            res["f"],
            s50,
            comments=[
                f"Order 0 openEMS 3D prediction: {model} (43 um copper, nominal FR408HR)",
                "renormalized from each port's line impedance to 50 ohm; e^(+jwt)",
            ],
        )
        z5 = {
            str(k + 1): round(float(np.interp(5e9, res["f"], np.real(res["z"][:, k]))), 3)
            for k in range(n)
        }
        summary[model] = dict(file=os.path.basename(path), runs=len(runs), z_at_5ghz_ohm=z5)
    with open(os.path.join(a.out, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=1)
    print(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
