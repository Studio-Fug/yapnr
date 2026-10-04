"""Touchstone 1.0 reader and writer for one- and two-port files (numpy only).

Two-port data rows are ordered S11 S21 S12 S22 (Touchstone 1.0). Frequencies are returned in Hz.
"""

from __future__ import annotations

import math
import os
from typing import List, Optional, Tuple

import numpy as np

_UNITS = {"hz": 1.0, "khz": 1e3, "mhz": 1e6, "ghz": 1e9}


def read(path: str) -> Tuple[np.ndarray, np.ndarray, float]:
    """(f in Hz, S of shape (F, n, n), reference resistance)."""
    ext = os.path.splitext(path)[1].lower()
    nports = int(ext[2:-1]) if ext.startswith(".s") and ext.endswith("p") else 2
    unit, fmt, ref = 1e9, "ma", 50.0
    vals: List[float] = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for raw in fh:
            line = raw.split("!", 1)[0].strip()
            if not line:
                continue
            if line.startswith("#"):
                tok = line[1:].lower().split()
                for i, t in enumerate(tok):
                    if t in _UNITS:
                        unit = _UNITS[t]
                    elif t in ("ri", "ma", "db"):
                        fmt = t
                    elif t == "r" and i + 1 < len(tok):
                        ref = float(tok[i + 1])
                continue
            if line.startswith("["):
                raise ValueError(f"{path}: Touchstone 2.0 keywords are not supported")
            vals += [float(x) for x in line.split()]
    per = 1 + 2 * nports * nports
    a = np.array(vals, float)
    if a.size % per:
        raise ValueError(f"{path}: {a.size} numbers is not a multiple of {per}")
    a = a.reshape(-1, per)
    f = a[:, 0] * unit
    p, q = a[:, 1::2], a[:, 2::2]
    if fmt == "ri":
        z = p + 1j * q
    elif fmt == "ma":
        z = p * np.exp(1j * np.radians(q))
    else:
        z = 10 ** (p / 20) * np.exp(1j * np.radians(q))
    s = z.reshape(-1, nports, nports)
    if nports == 2:
        s = s.transpose(0, 2, 1)  # file order S11 S21 S12 S22 is column-major
    return f, s, ref


def write(
    path: str,
    f: np.ndarray,
    s: np.ndarray,
    comments: Optional[List[str]] = None,
    fmt: str = "ma",
    digits: int = 5,
    ref: float = 50.0,
) -> None:
    s = np.asarray(s)
    if s.ndim == 1:
        s = s[:, None, None]
    n = s.shape[1]
    with open(path, "w", encoding="utf-8") as fh:
        for c in comments or []:
            fh.write(f"! {c}\n")
        fh.write(f"# GHz S {fmt.upper()} R {ref:g}\n")
        for k in range(len(f)):
            m = s[k].T if n == 2 else s[k]
            row = [f"{f[k] / 1e9:.10g}"]
            for z in m.reshape(-1):
                if fmt == "ri":
                    row += [f"{z.real:.{digits}g}", f"{z.imag:.{digits}g}"]
                else:
                    mag = abs(z)
                    if fmt == "db":
                        mag = 20 * math.log10(max(mag, 1e-30))
                    ang = math.degrees(math.atan2(z.imag, z.real))
                    row += [f"{mag:.{digits}g}", f"{ang:.{max(2, digits - 3)}f}"]
            fh.write(" ".join(row) + "\n")
